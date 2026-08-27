"""Archive-side runtime reduction and the surfaces still read from the archive.

The served timeline is projected from catalogd facts, not from these rows, so
nothing here asserts what ``/timeline/sessions`` renders — that contract lives
in ``test_live_catalog_timeline.py`` against a real catalog.

What remains is what the archive lane still owns:
- the runtime reducer's run/terminal semantics (a run ends without closing its
  session; a replayed terminal never rewrites a settled outcome)
- timeline-card projection: previews, staleness, and card-anchored ordering
- ``/agents/sessions/active``, which still hydrates candidates from the archive
"""

import asyncio
import json
import os
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import TimelineCard
from zerg.services.agents import AgentsStore
from zerg.services.session_hot_cards import upsert_timeline_card_from_session
from zerg.services.session_listing import SessionListParams
from zerg.services.session_listing import list_agent_sessions
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import build_timeline_cards_from_thread_rows
from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser
from zerg.session_execution_home import SessionExecutionHome


def _make_db(tmp_path, name="timeline_runtime_overlay.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    project: str = "zerg",
    environment: str = "production",
    user_messages: int = 2,
    assistant_messages: int = 2,
    tool_calls: int = 0,
    provider: str = "claude",
    execution_home: str | None = None,
    managed_transport: str | None = None,
):
    session = AgentSession(
        provider=provider,
        environment=environment,
        project=project,
        started_at=started_at,
        ended_at=ended_at,
                        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        summary="Timeline runtime test",
        summary_title="Timeline runtime test",
                                    )
    db.add(session)
    db.flush()
    db.refresh(session)
    if execution_home in {"managed_local", SessionExecutionHome.MANAGED_LOCAL.value}:
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        if managed_transport == "codex_app_server":
            kernel_plane = "codex_bridge"
        elif managed_transport == "opencode_process":
            kernel_plane = "opencode_process"
        else:
            kernel_plane = "claude_channel_bridge"
        seed_managed_kernel_rows(db, session, control_plane=kernel_plane)
    upsert_timeline_card_from_session(db, session)
    db.commit()
    db.refresh(session)
    return session


def _upsert_runtime_state(
    db,
    *,
    session_id: str,
    phase: str,
    updated_at: datetime,
    tool_name: str | None = None,
    provider: str = "claude",
    freshness_window: timedelta = timedelta(minutes=5),
):
    runtime_key = f"{provider}:{session_id}"
    row = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first()
    if row is None:
        row = SessionRuntimeState(
            runtime_key=runtime_key,
            session_id=session_id,
            provider=provider,
            phase=phase,
            phase_source="semantic",
            active_tool=tool_name,
            phase_started_at=updated_at,
            last_runtime_signal_at=updated_at,
            last_progress_at=updated_at,
            last_live_at=updated_at,
            timeline_anchor_at=updated_at,
            freshness_expires_at=updated_at + freshness_window,
            runtime_version=1,
        )
        db.add(row)
    else:
        row.phase = phase
        row.phase_source = "semantic"
        row.active_tool = tool_name
        row.phase_started_at = updated_at
        row.last_runtime_signal_at = updated_at
        row.last_progress_at = updated_at
        row.last_live_at = updated_at
        row.timeline_anchor_at = updated_at
        row.freshness_expires_at = updated_at + freshness_window
        row.runtime_version = (row.runtime_version or 0) + 1
    db.commit()


def _ingest_bridge_transcript(
    db,
    *,
    session_id,
    occurred_at: datetime,
    text: str,
    seq: int,
    turn_completed: bool = False,
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
                dedupe_key=f"bridge:live:{session_id}:thread-1:turn-1:{seq}",
                payload={
                    "progress_kind": "bridge_live_transcript_delta",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "seq": seq,
                    "method": "item/agentMessage/delta",
                    "delta": text[-1:],
                    "live_text": text,
                    "turn_completed": turn_completed,
                },
            )
        ],
    )
    db.commit()


def _client(factory):
    from zerg.main import api_app

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="timeline-runtime", id="token-1", owner_id=1)

    def override_browser_user():
        return SimpleNamespace(id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[get_current_browser_user] = override_browser_user
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        yield TestClient(api_app)
    finally:
        api_app.dependency_overrides.clear()


def test_run_terminal_signal_closes_run_connection_not_session(tmp_path):
    factory = _make_db(tmp_path, "run_terminal_closes_kernel_run.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        from zerg.services.agents.kernel_writes import ensure_primary_thread
        from zerg.services.agents.kernel_writes import record_run
        from zerg.services.agents.kernel_writes import upsert_connection_for_run

        session = _seed_session(db, started_at=now - timedelta(minutes=30), ended_at=None, provider="codex")
        thread = ensure_primary_thread(db, session)
        run = record_run(
            db,
            thread=thread,
            provider="codex",
            host_id="cinder",
            cwd="/Users/me/repo",
            launch_origin="longhouse_spawned",
        )
        conn = upsert_connection_for_run(
            db,
            run=run,
            control_plane="codex_exec",
            acquisition_kind="spawned_control",
            state="attached",
            external_name="cinder",
            can_send_input=0,
            can_interrupt=0,
            can_terminate=0,
            can_tail_output=0,
            can_resume=0,
        )
        db.commit()
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{session.id}",
                    session_id=session.id,
                    thread_id=thread.id,
                    run_id=run.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_exec",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key=f"codex-exec:{run.id}:terminal",
                    payload={"terminal_state": "run_completed", "exit_code": 0},
                )
            ],
        )
        db.commit()
        db.refresh(session)
        refreshed_run = db.get(SessionRun, run.id)
        refreshed_conn = db.get(SessionConnection, conn.id)
        runtime = db.get(SessionRuntimeState, f"codex:{session.id}")

        assert session.ended_at is None
        assert refreshed_run.ended_at is not None
        assert refreshed_run.exit_status == "exit_0"
        assert refreshed_conn.state == "ended"
        assert refreshed_conn.released_at is not None
        assert runtime.run_id == run.id
        assert runtime.terminal_state == "run_completed"
    finally:
        db.close()


def test_run_terminal_signal_does_not_overwrite_existing_run_outcome(tmp_path):
    factory = _make_db(tmp_path, "run_terminal_idempotent.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        from zerg.services.agents.kernel_writes import ensure_primary_thread
        from zerg.services.agents.kernel_writes import record_run
        from zerg.services.agents.kernel_writes import upsert_connection_for_run

        session = _seed_session(db, started_at=now - timedelta(minutes=30), ended_at=None, provider="codex")
        thread = ensure_primary_thread(db, session)
        run = record_run(
            db,
            thread=thread,
            provider="codex",
            host_id="cinder",
            cwd="/Users/me/repo",
            launch_origin="longhouse_spawned",
        )
        conn = upsert_connection_for_run(
            db,
            run=run,
            control_plane="codex_exec",
            acquisition_kind="spawned_control",
            state="attached",
            external_name="cinder",
        )
        db.commit()
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{session.id}",
                    session_id=session.id,
                    thread_id=thread.id,
                    run_id=run.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_exec",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key=f"codex-exec:{run.id}:terminal:complete",
                    payload={"terminal_state": "run_completed", "exit_code": 0},
                ),
                RuntimeEventIngest(
                    runtime_key=f"codex:{session.id}",
                    session_id=session.id,
                    thread_id=thread.id,
                    run_id=run.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_exec",
                    kind="terminal_signal",
                    occurred_at=now + timedelta(seconds=1),
                    dedupe_key=f"codex-exec:{run.id}:terminal:failed-replay",
                    payload={"terminal_state": "run_failed", "exit_code": 1},
                ),
            ],
        )
        db.commit()
        refreshed_run = db.get(SessionRun, run.id)
        refreshed_conn = db.get(SessionConnection, conn.id)
        runtime = db.get(SessionRuntimeState, f"codex:{session.id}")

        assert refreshed_run.ended_at is not None
        assert refreshed_run.exit_status == "exit_0"
        assert refreshed_conn.state == "ended"
        assert runtime.terminal_state == "run_completed"
    finally:
        db.close()


def test_late_phase_and_progress_do_not_reopen_ended_run_without_new_run_id(tmp_path):
    factory = _make_db(tmp_path, "ended_run_rejects_late_activity.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=30), ended_at=None)
        session_id = str(session.id)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"claude:{session_id}",
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="terminal:process-gone",
                    payload={"terminal_state": "process_gone"},
                ),
                RuntimeEventIngest(
                    runtime_key=f"claude:{session_id}",
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now + timedelta(seconds=1),
                    freshness_ms=90_000,
                    dedupe_key="phase:late-after-process-gone",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=f"claude:{session_id}",
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="progress_signal",
                    occurred_at=now + timedelta(seconds=2),
                    dedupe_key="progress:late-after-process-gone",
                    payload={"progress_kind": "transcript_append"},
                ),
            ],
        )
        db.commit()

        runtime_state = db.query(SessionRuntimeState).filter_by(runtime_key=f"claude:{session_id}").one()
        assert runtime_state.terminal_state == "process_gone"
        assert runtime_state.phase == "finished"
    finally:
        db.close()


def test_new_run_id_may_replace_terminal_state_with_new_activity(tmp_path):
    factory = _make_db(tmp_path, "new_run_may_replace_terminal.db")
    now = datetime.now(timezone.utc)
    previous_run_id = uuid4()
    next_run_id = uuid4()

    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=30), ended_at=None)
        session_id = str(session.id)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"claude:{session_id}",
                    session_id=session.id,
                    run_id=previous_run_id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="terminal_signal",
                    occurred_at=now,
                    dedupe_key="terminal:previous-run",
                    payload={"terminal_state": "process_gone"},
                ),
                RuntimeEventIngest(
                    runtime_key=f"claude:{session_id}",
                    session_id=session.id,
                    run_id=next_run_id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now + timedelta(seconds=1),
                    freshness_ms=90_000,
                    dedupe_key="phase:new-run",
                    payload={},
                ),
            ],
        )
        db.commit()

        runtime_state = db.query(SessionRuntimeState).filter_by(runtime_key=f"claude:{session_id}").one()
        assert runtime_state.run_id == next_run_id
        assert runtime_state.terminal_state is None
        assert runtime_state.phase == "thinking"
    finally:
        db.close()


def test_timeline_compatibility_cards_include_bridge_transcript_preview(tmp_path):
    factory = _make_db(tmp_path, "codex_bridge_transcript_timeline_compat.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-live-compat",
            started_at=now - timedelta(minutes=10),
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="codex_app_server",
        )
        _ingest_bridge_transcript(
            db,
            session_id=session.id,
            occurred_at=now - timedelta(milliseconds=50),
            text="timeline compat",
            seq=5,
        )

        result = asyncio.run(
            list_timeline_sessions_for_browser(
                db=db,
                params=TimelineSessionListParams(
                    project="codex-live-compat",
                    provider="codex",
                    environment=None,
                    include_test=False,
                    hide_autonomous=True,
                    device_id=None,
                    days_back=14,
                    query=None,
                    limit=5,
                    offset=0,
                    sort=None,
                    mode="semantic",
                    context_mode="forensic",
                ),
            )
        )
    finally:
        db.close()

    assert result.compatibility_raw is True
    assert result.response.sessions[0].id == str(session.id)
    assert result.response.sessions[0].transcript_preview is not None
    assert result.response.sessions[0].transcript_preview.text == "timeline compat"
    assert result.response.sessions[0].transcript_preview.is_stale is False


def test_timeline_cards_read_projection_not_large_observation_history(tmp_path):
    factory = _make_db(tmp_path, "codex_bridge_transcript_projection_hot_path.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-live-hot-path",
            started_at=now - timedelta(minutes=10),
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="codex_app_server",
        )
        _ingest_bridge_transcript(
            db,
            session_id=session.id,
            occurred_at=now,
            text="projection preview",
            seq=2000,
        )
        payload = {
            "kind": "progress_signal",
            "payload": {
                "progress_kind": "bridge_live_transcript_delta",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "seq": 1,
                "live_text": "x" * 2048,
            },
        }
        db.bulk_save_objects(
            [
                SessionObservation(
                    observation_id=f"runtime:codex_bridge_live:history:{session.id}:{idx}",
                    session_id=session.id,
                    runtime_key=f"codex:{session.id}",
                    provider="codex",
                    source_domain="runtime",
                    source="codex_bridge_live",
                    kind=OBS_KIND_BRIDGE_TRANSCRIPT_DELTA,
                    observed_at=now - timedelta(seconds=idx + 1),
                    received_at=now - timedelta(seconds=idx + 1),
                    payload_json=json.dumps(payload),
                )
                for idx in range(1200)
            ]
        )
        db.commit()

        statements: list[str] = []

        def _collect_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        bind = db.get_bind()
        sqlalchemy_event.listen(bind, "before_cursor_execute", _collect_statement)
        started = monotonic()
        try:
            cards = build_timeline_cards_from_thread_rows(
                db=db,
                thread_rows=((str(session.id), str(session.id), now),),
            )
        finally:
            elapsed = monotonic() - started
            sqlalchemy_event.remove(bind, "before_cursor_execute", _collect_statement)
    finally:
        db.close()

    assert cards[0].head.transcript_preview is not None
    assert cards[0].head.transcript_preview.text == "projection preview"
    assert elapsed < 0.5
    assert not any("session_observations" in statement.lower() for statement in statements)


def test_no_query_session_lists_do_not_touch_cold_archive_tables(tmp_path, monkeypatch):
    factory = _make_db(tmp_path, "hot_list_cold_table_guard.db")
    now = datetime.now(timezone.utc)

    def _store_unavailable(*_args, **_kwargs):
        raise RuntimeError("cold store unavailable")

    monkeypatch.setattr("zerg.data_plane.create_archive_store", _store_unavailable)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="hot-list-cold-guard",
            started_at=now - timedelta(minutes=5),
            user_messages=1,
            assistant_messages=1,
        )
        session.first_user_message_preview = "hot preview"
        session.last_visible_text_preview = "hot latest"
        missing_preview_session = _seed_session(
            db,
            provider="codex",
            project="hot-list-cold-guard",
            started_at=now - timedelta(minutes=4),
            user_messages=1,
            assistant_messages=1,
        )
        db.add(
            AgentSourceLine(
                session_id=session.id,
                thread_id=None,
                source_path="/tmp/cold-archive.jsonl",
                source_offset=1,
                branch_id=0,
                raw_json='{"type":"cold"}',
                line_hash="hot-list-cold-guard",
            )
        )
        db.add(
            AgentEvent(
                session_id=missing_preview_session.id,
                role="user",
                content_text="cold event fallback should not be queried",
                timestamp=now - timedelta(minutes=3),
            )
        )
        db.commit()

        statements: list[str] = []

        def _collect_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        bind = db.get_bind()
        sqlalchemy_event.listen(bind, "before_cursor_execute", _collect_statement)
        try:
            agent_result = asyncio.run(
                list_agent_sessions(
                    db=db,
                    auth=SimpleNamespace(device_id="timeline-runtime", id="token-1", owner_id=1),
                    params=SessionListParams(
                        project="hot-list-cold-guard",
                        provider="codex",
                        environment=None,
                        include_test=False,
                        hide_autonomous=False,
                        device_id=None,
                        days_back=90,
                        query=None,
                        limit=5,
                        offset=0,
                        sort=None,
                        mode="lexical",
                        context_mode="forensic",
                    ),
                    owner_id=1,
                )
            )
            timeline_result = asyncio.run(
                list_timeline_sessions_for_browser(
                    db=db,
                    params=TimelineSessionListParams(
                        project="hot-list-cold-guard",
                        provider="codex",
                        environment=None,
                        include_test=False,
                        hide_autonomous=False,
                        device_id=None,
                        days_back=90,
                        query=None,
                        limit=5,
                        offset=0,
                        sort=None,
                        mode="lexical",
                        context_mode="forensic",
                    ),
                    owner_id=1,
                )
            )
        finally:
            sqlalchemy_event.remove(bind, "before_cursor_execute", _collect_statement)
    finally:
        db.close()

    agent_first_user_by_id = {item.id: item.first_user_message for item in agent_result.response.sessions}
    assert agent_first_user_by_id[str(session.id)] == "hot preview"
    assert agent_first_user_by_id[str(missing_preview_session.id)] is None
    assert len(timeline_result.response.sessions) == 2
    rendered_sql = re.sub(r"\s+", " ", " ".join(statement.lower() for statement in statements))
    assert "timeline_cards" in rendered_sql
    assert "source_lines" not in rendered_sql
    assert "events_fts" not in rendered_sql
    assert " from events" not in rendered_sql
    assert " join events" not in rendered_sql


def test_no_query_session_list_orders_by_timeline_card_activity(tmp_path):
    factory = _make_db(tmp_path, "timeline_card_session_list_order.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session_recent_in_session = _seed_session(
            db,
            provider="codex",
            project="card-order",
            started_at=now - timedelta(minutes=5),
            user_messages=1,
            assistant_messages=1,
        )
        session_recent_in_session.last_activity_at = now - timedelta(seconds=5)
        session_recent_in_card = _seed_session(
            db,
            provider="codex",
            project="card-order",
            started_at=now - timedelta(minutes=10),
            user_messages=1,
            assistant_messages=1,
        )
        session_recent_in_card.last_activity_at = now - timedelta(days=1)
        db.query(TimelineCard).filter(TimelineCard.session_id == session_recent_in_session.id).update(
            {"last_activity_at": now - timedelta(days=2)}
        )
        db.query(TimelineCard).filter(TimelineCard.session_id == session_recent_in_card.id).update(
            {"last_activity_at": now}
        )
        db.commit()

        result = asyncio.run(
            list_agent_sessions(
                db=db,
                auth=SimpleNamespace(device_id="timeline-runtime", id="token-1", owner_id=1),
                params=SessionListParams(
                    project="card-order",
                    provider="codex",
                    environment=None,
                    include_test=False,
                    hide_autonomous=False,
                    device_id=None,
                    days_back=90,
                    query=None,
                    limit=5,
                    offset=0,
                    sort=None,
                    mode="lexical",
                    context_mode="forensic",
                ),
                owner_id=1,
            )
        )
    finally:
        db.close()

    assert [row.id for row in result.response.sessions] == [
        str(session_recent_in_card.id),
        str(session_recent_in_session.id),
    ]


def test_no_query_timeline_thread_page_orders_by_timeline_card_activity(tmp_path):
    factory = _make_db(tmp_path, "timeline_card_thread_order.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session_recent_in_session = _seed_session(
            db,
            provider="codex",
            project="card-thread-order",
            started_at=now - timedelta(minutes=5),
            user_messages=1,
            assistant_messages=1,
        )
        session_recent_in_session.last_activity_at = now - timedelta(seconds=5)
        session_recent_in_card = _seed_session(
            db,
            provider="codex",
            project="card-thread-order",
            started_at=now - timedelta(minutes=10),
            user_messages=1,
            assistant_messages=1,
        )
        session_recent_in_card.last_activity_at = now - timedelta(days=1)
        db.query(TimelineCard).filter(TimelineCard.session_id == session_recent_in_session.id).update(
            {"last_activity_at": now - timedelta(days=2)}
        )
        db.query(TimelineCard).filter(TimelineCard.session_id == session_recent_in_card.id).update(
            {"last_activity_at": now}
        )
        db.commit()

        total, rows = AgentsStore(db).list_timeline_thread_page(
            project="card-thread-order",
            provider="codex",
            hide_autonomous=False,
            since=now - timedelta(days=90),
        )
    finally:
        db.close()

    assert total == 2
    assert [session_id for _thread_id, session_id, _anchor in rows] == [
        str(session_recent_in_card.id),
        str(session_recent_in_session.id),
    ]


def test_timeline_cards_mark_old_unsuperseded_bridge_transcript_stale(tmp_path):
    factory = _make_db(tmp_path, "codex_bridge_transcript_stale.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-bridge-preview-stale",
            started_at=now - timedelta(minutes=10),
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="codex_app_server",
        )
        _ingest_bridge_transcript(
            db,
            session_id=session.id,
            occurred_at=now - timedelta(minutes=5),
            text="old partial",
            seq=2,
        )

        cards = build_timeline_cards_from_thread_rows(
            db=db,
            thread_rows=((str(session.id), str(session.id), now),),
        )
    finally:
        db.close()
    preview = cards[0].head.transcript_preview
    assert preview is not None
    assert preview.text == "old partial"
    assert preview.is_provisional is True
    assert preview.is_stale is True
    assert preview.stale_reason == "freshness_window_expired"


def test_timeline_cards_mark_preview_stale_when_durable_activity_is_newer(tmp_path):
    factory = _make_db(tmp_path, "codex_bridge_transcript_durable_newer.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-bridge-preview-durable-newer",
            started_at=now - timedelta(minutes=10),
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="codex_app_server",
        )
        _ingest_bridge_transcript(
            db,
            session_id=session.id,
            occurred_at=now - timedelta(seconds=30),
            text="older bridge preview",
            seq=2,
        )
        session.last_activity_at = now - timedelta(seconds=5)
        db.commit()

        cards = build_timeline_cards_from_thread_rows(
            db=db,
            thread_rows=((str(session.id), str(session.id), now),),
        )
    finally:
        db.close()
    preview = cards[0].head.transcript_preview
    assert preview is not None
    assert preview.text == "older bridge preview"
    assert preview.is_stale is True
    assert preview.stale_reason == "superseded_by_durable"


def test_active_sessions_fresh_presence_beats_ended_at(tmp_path):
    factory = _make_db(tmp_path, "presence_beats_ended.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(minutes=2),
            project="fresh-presence",
        )
        _upsert_runtime_state(
            db,
            session_id=str(session.id),
            phase="thinking",
            updated_at=now - timedelta(seconds=15),
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["sessions"]
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == str(session.id)
        assert row["status"] == "completed"
        assert row["presence_state"] is None
        assert row["display_phase"] is None
        assert row["confidence"] is None
        assert row["timeline_anchor_at"] is not None


def test_archive_active_sessions_ignore_runtime_only_recency_anchor(tmp_path):
    factory = _make_db(tmp_path, "runtime_anchor_active.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        old_runtime = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-runtime-active",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{old_runtime.id}",
                session_id=old_runtime.id,
                provider="claude",
                device_id="cinder",
                phase="thinking",
                phase_source="semantic",
                active_tool=None,
                phase_started_at=now - timedelta(seconds=10),
                last_runtime_signal_at=now - timedelta(seconds=10),
                last_progress_at=now - timedelta(seconds=10),
                last_live_at=now - timedelta(seconds=10),
                timeline_anchor_at=now - timedelta(seconds=10),
                freshness_expires_at=now + timedelta(minutes=1),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["sessions"]
        assert rows == []


def test_active_sessions_recent_progress_fallback_is_non_executing(tmp_path):
    factory = _make_db(tmp_path, "active_sessions_progress_fallback.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=1),
            ended_at=None,
            project="progress-fallback",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["sessions"]
        row = next(item for item in rows if item["id"] == str(session.id))
        assert row["status"] == "active"
        assert row["presence_state"] is None
        assert row["display_phase"] is None
        assert row["runtime_phase"] is None
        assert row["confidence"] is None
        assert row["runtime_display"]["truth_tier"] == "none"
        assert row["runtime_display"]["headline"] == "Inactive"
        assert row["runtime_display"]["phase_label"] == "Inactive"
