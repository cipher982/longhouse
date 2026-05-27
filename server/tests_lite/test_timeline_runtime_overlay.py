"""HTTP-level tests for timeline runtime overlay and recent-activity ordering.

Covers:
- /agents/sessions uses recent activity anchor (presence/event activity), not raw started_at
- Open sessions without fresh live signals return an idle runtime overlay instead of implicit active
- Fresh presence overrides ended_at for /agents/sessions/active
"""

import asyncio
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from time import monotonic
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_views import _latest_source_line_path_for_native_continue
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import build_timeline_cards_from_thread_rows
from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser
from zerg.session_execution_home import SessionExecutionHome

# UnmanagedSessionBinding was removed in the session-identity-kernel cleanup;
# the two tests that seeded it are skipped further down. Stub the symbol so
# any stray references during collection don't NameError.
UnmanagedSessionBinding = None  # type: ignore[assignment]


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
    continuation_kind: str | None = None,
    origin_label: str | None = None,
    user_messages: int = 2,
    assistant_messages: int = 2,
    tool_calls: int = 0,
    provider: str = "claude",
    execution_home: str | None = None,
    managed_transport: str | None = None,
    source_runner_id: int | None = None,
    managed_session_name: str | None = None,
):
    session = AgentSession(
        provider=provider,
        environment=environment,
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        continuation_kind=continuation_kind,
        origin_label=origin_label,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        summary="Timeline runtime test",
        summary_title="Timeline runtime test",
        execution_home=execution_home,
        managed_transport=managed_transport,
        source_runner_id=source_runner_id,
        managed_session_name=managed_session_name,
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

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    try:
        yield TestClient(api_app)
    finally:
        api_app.dependency_overrides.clear()


@pytest.mark.parametrize("terminal_state", ["session_ended", "process_gone", "user_closed"])
def test_terminal_signals_with_irreversible_states_close_session_and_timeline(tmp_path, terminal_state):
    factory = _make_db(tmp_path, f"terminal_closes_{terminal_state}.db")
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
                    dedupe_key=f"terminal:{terminal_state}",
                    payload={"terminal_state": terminal_state},
                )
            ],
        )
        db.commit()
        db.refresh(session)

        assert session.ended_at is not None
        assert session.ended_at.replace(tzinfo=timezone.utc) == now
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == session_id
        assert row["runtime_display"]["lifecycle"] == "closed"
        assert row["timeline_card"]["status"]["label"] == "Closed"
        assert row["timeline_card"]["status"]["tone"] == "closed"


@pytest.mark.parametrize("terminal_state", ["finished", "host_expired"])
def test_reversible_or_turn_terminal_signals_do_not_close_session(tmp_path, terminal_state):
    factory = _make_db(tmp_path, f"terminal_open_{terminal_state}.db")
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
                    dedupe_key=f"terminal:{terminal_state}",
                    payload={"terminal_state": terminal_state},
                )
            ],
        )
        db.commit()
        db.refresh(session)

        assert session.ended_at is None
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == session_id
        assert row["runtime_display"]["lifecycle"] == "unknown"
        assert row["timeline_card"]["status"]["label"] == "No live signal"
        assert row["timeline_card"]["status"]["seen_at"] is not None
        assert row["timeline_card"]["status"]["seen_at_prefix"] == "Last signal"


def test_progress_after_host_expired_reopens_runtime_projection(tmp_path):
    factory = _make_db(tmp_path, "host_expired_then_progress.db")
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
                    dedupe_key="terminal:host_expired",
                    payload={"terminal_state": "host_expired"},
                ),
                RuntimeEventIngest(
                    runtime_key=f"claude:{session_id}",
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="progress_signal",
                    occurred_at=now + timedelta(seconds=5),
                    dedupe_key="progress:after-host-expired",
                    payload={"progress_kind": "transcript_append"},
                ),
            ],
        )
        db.commit()
        db.refresh(session)

        runtime_state = db.query(SessionRuntimeState).filter_by(runtime_key=f"claude:{session_id}").one()
        assert session.ended_at is None
        assert runtime_state.terminal_state is None
        assert runtime_state.phase == "idle"
        assert runtime_state.phase_source == "progress"
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == session_id
        assert row["timeline_card"]["status"]["label"] == "No live signal"


def test_sessions_list_uses_recent_activity_anchor_for_old_live_session(tmp_path):
    factory = _make_db(tmp_path, "recent_anchor.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        old_live = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-live",
        )
        recent_idle = _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1, minutes=30),
            project="recent-idle",
        )
        _upsert_runtime_state(
            db,
            session_id=str(old_live.id),
            phase="running",
            updated_at=now - timedelta(seconds=20),
            tool_name="bash",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["total"] >= 1
        top = payload["sessions"][0]
        assert top["id"] == str(old_live.id)
        assert top["project"] == "old-live"
        assert top["status"] == "working"
        assert top["presence_state"] == "running"
        assert top["active_tool"] == "bash"
        assert top["display_phase"] == "Running bash"
        assert top["confidence"] == "live"
        assert top["runtime_display"] == {
            "truth_tier": "fresh",
            "signal_tier": "phase_signal",
            "state": "running",
            "tone": "running",
            "headline": "Active",
            "detail": None,
            "phase_label": "Using Shell",
            "compact_tool_label": "Shell",
            "is_live": True,
            "is_executing": True,
            "needs_attention": False,
            "is_idle": False,
            "is_stalled": False,
            "is_managed_local_truth": False,
            "has_signal": True,
            "control_path": "unmanaged",
            "activity_recency": "live",
            "lifecycle": "open",
            "host_state": "unknown",
            "terminal_reason": None,
        }
        assert top["timeline_card"]["status"]["label"] == "Using Shell"
        assert top["timeline_card"]["status"]["tone"] == "running"
        assert top["timeline_anchor_at"] is not None
        assert top["timeline_anchor_at"] >= recent_idle.started_at.isoformat().replace("+00:00", "Z")


def test_bridge_transcript_preview_is_timeline_card_only(tmp_path):
    factory = _make_db(tmp_path, "codex_bridge_transcript_preview.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-bridge-preview",
            started_at=now - timedelta(minutes=10),
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="codex_app_server",
        )
        _upsert_runtime_state(
            db,
            session_id=str(session.id),
            phase="idle",
            updated_at=now - timedelta(seconds=2),
            provider="codex",
        )
        _ingest_bridge_transcript(
            db,
            session_id=session.id,
            occurred_at=now - timedelta(milliseconds=80),
            text="hello world",
            seq=4,
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get(
            "/agents/sessions?project=codex-bridge-preview&provider=codex&limit=5",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text
        session_payload = resp.json()["sessions"][0]

    assert session_payload["id"] == str(session.id)
    assert session_payload["transcript_preview"]["text"] == "hello world"
    assert session_payload["transcript_preview"]["event_origin"] == "live_provisional"
    assert session_payload["transcript_preview"]["is_provisional"] is True
    assert session_payload["transcript_preview"]["is_stale"] is False

    db = factory()
    try:
        cards = build_timeline_cards_from_thread_rows(
            db=db,
            thread_rows=((str(session.thread_root_session_id or session.id), str(session.id), now),),
        )
    finally:
        db.close()
    assert cards[0].head.transcript_preview is not None
    assert cards[0].head.transcript_preview.text == "hello world"
    assert cards[0].head.transcript_preview.content_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1:4"
    assert cards[0].head.transcript_preview.is_provisional is True
    assert cards[0].head.transcript_preview.is_stale is False


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
                thread_rows=((str(session.thread_root_session_id or session.id), str(session.id), now),),
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


def test_native_continue_source_line_fallback_uses_session_only_lookup(tmp_path):
    factory = _make_db(tmp_path, "native_continue_source_line_lookup.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-native-continue-source",
            started_at=now - timedelta(minutes=10),
        )
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        thread, _run, _connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        db.add_all(
            [
                AgentSourceLine(
                    session_id=session.id,
                    thread_id=thread.id,
                    source_path="/tmp/older-thread.jsonl",
                    source_offset=1,
                    branch_id=0,
                    raw_json="{}",
                    line_hash="native-continue-older",
                ),
                AgentSourceLine(
                    session_id=session.id,
                    thread_id=None,
                    source_path="/tmp/latest-session.jsonl",
                    source_offset=2,
                    branch_id=0,
                    raw_json="{}",
                    line_hash="native-continue-latest",
                ),
            ]
        )
        db.commit()

        statements: list[str] = []

        def _collect_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        bind = db.get_bind()
        sqlalchemy_event.listen(bind, "before_cursor_execute", _collect_statement)
        try:
            source_path = _latest_source_line_path_for_native_continue(db, session_id=session.id)
        finally:
            sqlalchemy_event.remove(bind, "before_cursor_execute", _collect_statement)
    finally:
        db.close()

    assert source_path == "/tmp/latest-session.jsonl"
    source_statements = " ".join(statement.lower() for statement in statements if "source_lines" in statement.lower())
    assert "thread_id" not in source_statements


def test_sessions_list_hides_bridge_transcript_preview_after_durable_activity_catches_up(tmp_path):
    factory = _make_db(tmp_path, "codex_bridge_transcript_superseded.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            provider="codex",
            project="codex-bridge-preview-superseded",
            started_at=now - timedelta(minutes=10),
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="codex_app_server",
        )
        _ingest_bridge_transcript(
            db,
            session_id=session.id,
            occurred_at=now - timedelta(seconds=5),
            text="older partial",
            seq=4,
        )
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session.id,
                provider="codex",
                environment="production",
                project="codex-bridge-preview-superseded",
                started_at=session.started_at,
                execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="older partial finalized in durable transcript",
                        timestamp=now - timedelta(seconds=1),
                        source_path="/tmp/codex-rollout.jsonl",
                        source_offset=1,
                        raw_json='{"type":"response_item"}',
                    )
                ],
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get(
            "/agents/sessions?project=codex-bridge-preview-superseded&provider=codex&limit=5",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text
        session_payload = resp.json()["sessions"][0]

    assert session_payload["id"] == str(session.id)
    assert session_payload["last_activity_at"] == (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    assert session_payload["transcript_preview"] is None

    db = factory()
    try:
        cards = build_timeline_cards_from_thread_rows(
            db=db,
            thread_rows=((str(session.thread_root_session_id or session.id), str(session.id), now),),
        )
    finally:
        db.close()
    assert cards[0].head.transcript_preview is None


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
            thread_rows=((str(session.thread_root_session_id or session.id), str(session.id), now),),
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
            thread_rows=((str(session.thread_root_session_id or session.id), str(session.id), now),),
        )
    finally:
        db.close()
    preview = cards[0].head.transcript_preview
    assert preview is not None
    assert preview.text == "older bridge preview"
    assert preview.is_stale is True
    assert preview.stale_reason == "superseded_by_durable"


def test_sessions_list_marks_old_open_session_idle_without_live_signal(tmp_path):
    factory = _make_db(tmp_path, "open_idle.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(days=3),
            ended_at=None,
            project="open-idle",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["sessions"][0]
        assert data["id"] == str(session.id)
        assert data["status"] == "idle"
        assert data["display_phase"] == "Inactive"
        assert data["presence_state"] is None
        assert data["confidence"] is None


def test_sessions_list_exposes_home_label_from_existing_session_metadata(tmp_path):
    factory = _make_db(tmp_path, "execution_home.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        unmanaged = _seed_session(
            db,
            started_at=now - timedelta(hours=4),
            ended_at=now - timedelta(hours=3),
            project="unmanaged-local",
            origin_label="cinder",
            environment="production",
        )
        cloud = _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            project="cloud-branch",
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="Cloud",
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=10", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        rows = {row["project"]: row for row in resp.json()["sessions"]}
        assert rows["unmanaged-local"]["id"] == str(unmanaged.id)
        assert rows["unmanaged-local"]["home_label"] is None
        assert rows["cloud-branch"]["id"] == str(cloud.id)
        assert rows["cloud-branch"]["home_label"] is None  # cloud labels hidden for launch


def test_managed_session_capability_needs_current_runner_truth(tmp_path):
    factory = _make_db(tmp_path, "managed_capability_requires_runner.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=10),
            ended_at=None,
            project="managed-without-runner",
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            managed_session_name="lh-managed-without-runner",
        )
        _upsert_runtime_state(
            db,
            session_id=str(session.id),
            phase="idle",
            updated_at=now,
        )
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["sessions"][0]
        assert data["id"] == str(session.id)
        assert data["runtime_display"]["control_path"] == "managed"
        assert data["runtime_display"]["activity_recency"] == "live"
        assert data["runtime_display"]["host_state"] == "unknown"
        assert data["capabilities"]["live_control_available"] is False
        assert data["capabilities"]["reply_to_live_session_available"] is False
        assert data["capabilities"]["can_queue_next_input"] is False
        assert data["capabilities"]["display_label"] == "Control offline"
        assert not str(data["capabilities"]["display_label"]).startswith("Live on")


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
        assert row["status"] == "working"
        assert row["presence_state"] == "thinking"
        assert row["display_phase"] == "Thinking"
        assert row["confidence"] == "live"
        assert row["timeline_anchor_at"] is not None


def test_sessions_list_uses_runtime_anchor_for_old_runtime_only_session(tmp_path):
    factory = _make_db(tmp_path, "runtime_anchor_sessions.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        old_runtime = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-runtime",
        )
        _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            project="recent-history",
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
                last_progress_at=now - timedelta(seconds=15),
                last_live_at=now - timedelta(seconds=30),
                timeline_anchor_at=now - timedelta(seconds=15),
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=2,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["total"] >= 1
        row = payload["sessions"][0]
        assert row["id"] == str(old_runtime.id)
        assert row["project"] == "old-runtime"
        assert row["status"] == "working"
        assert row["display_phase"] == "Running bash"
        assert row["timeline_anchor_at"] is not None
        assert row["timeline_card"]["status"]["label"] == "Using Shell"


def test_active_sessions_uses_runtime_anchor_for_old_runtime_only_session(tmp_path):
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
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == str(old_runtime.id)
        assert row["status"] == "working"
        assert row["presence_state"] == "thinking"
        assert row["display_phase"] == "Thinking"


def test_sessions_list_prefers_materialized_runtime_state_when_present(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_state.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(days=7),
            ended_at=now - timedelta(minutes=1),
            project="runtime-state",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
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
                runtime_version=3,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "working"
        assert row["presence_state"] == "running"
        assert row["display_phase"] == "Running bash"
        assert row["runtime_phase"] == "running"
        assert row["runtime_source"] == "semantic"
        assert row["runtime_version"] == 3
        assert row["confidence"] == "live"
        assert row["timeline_card"]["status"]["label"] == "Using Shell"


def test_sessions_list_keeps_progress_runtime_overlay_for_recent_closed_session(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_progress.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(days=2),
            ended_at=now - timedelta(minutes=1),
            project="runtime-progress",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="idle",
                phase_source="progress",
                active_tool=None,
                phase_started_at=now - timedelta(minutes=1),
                last_runtime_signal_at=None,
                last_progress_at=now - timedelta(seconds=20),
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=now - timedelta(seconds=20),
                freshness_expires_at=now - timedelta(seconds=1),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "idle"
        assert row["display_phase"] == "Inactive"
        assert row["runtime_phase"] is None
        assert row["runtime_source"] == "progress"
        assert row["presence_state"] is None
        assert row["last_live_at"] is None
        assert row["confidence"] == "stale"
        assert row["timeline_card"]["status"]["label"] == "No live signal"
        assert row["timeline_card"]["status"]["seen_at_prefix"] == "Checked"


def test_sessions_list_suppresses_stale_progress_running_phase(tmp_path):
    factory = _make_db(tmp_path, "stale_progress_running_overlay.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            project="stale-opencode",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"opencode:{session.id}",
                session_id=session.id,
                provider="opencode",
                device_id="cinder",
                phase="running",
                phase_source="progress",
                active_tool=None,
                phase_started_at=now - timedelta(hours=2),
                last_runtime_signal_at=None,
                last_progress_at=now - timedelta(hours=2),
                last_live_at=now - timedelta(hours=2),
                timeline_anchor_at=now - timedelta(hours=2),
                freshness_expires_at=None,
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "idle"
        assert row["presence_state"] is None
        assert row["display_phase"] == "Inactive"
        assert row["runtime_phase"] is None
        assert row["runtime_source"] == "progress"
        assert row["runtime_display"]["headline"] == "Inactive"
        assert row["runtime_display"]["phase_label"] == "Inactive"
        assert row["runtime_display"]["state"] is None
        assert row["runtime_display"]["activity_recency"] == "stale"
        assert row["runtime_display"]["tone"] == "inactive"


def test_sessions_list_suppresses_stale_phase_signal_from_timeline_status(tmp_path):
    factory = _make_db(tmp_path, "stale_phase_signal_timeline_status.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            project="stale-phase-signal",
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="claude_channel_bridge",
            source_runner_id=1,
            managed_session_name="claude",
        )
        db.add(
            AgentHeartbeat(
                device_id="timeline-runtime",
                received_at=now,
            )
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="timeline-runtime",
                phase="thinking",
                phase_source="managed_local_transport",
                active_tool=None,
                phase_started_at=now - timedelta(minutes=30),
                last_runtime_signal_at=now - timedelta(minutes=30),
                last_progress_at=now - timedelta(minutes=30),
                last_live_at=now - timedelta(minutes=30),
                timeline_anchor_at=now - timedelta(minutes=30),
                freshness_expires_at=now - timedelta(minutes=15),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=7&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["confidence"] == "stale"
        assert row["runtime_phase"] is None
        assert row["timeline_card"]["status"]["label"] == "No live signal"
        assert row["timeline_card"]["status"]["seen_at"] is not None
        assert row["timeline_card"]["status"]["seen_at_prefix"] == "Last signal"


def test_sessions_list_marks_materialized_needs_user_as_idle(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_needs_user.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=6),
            ended_at=None,
            project="runtime-needs-user",
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="claude_channel_bridge",
            source_runner_id=1,
            managed_session_name="claude",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="needs_user",
                phase_source="managed_local_transport",
                active_tool=None,
                phase_started_at=now - timedelta(seconds=30),
                last_runtime_signal_at=now - timedelta(seconds=30),
                last_progress_at=now - timedelta(seconds=25),
                last_live_at=now - timedelta(seconds=30),
                timeline_anchor_at=now - timedelta(seconds=25),
                freshness_expires_at=now + timedelta(minutes=10),
                terminal_state=None,
                terminal_at=None,
                runtime_version=4,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["status"] == "idle"
        assert row["presence_state"] == "needs_user"
        assert row["display_phase"] == "Idle"
        assert row["runtime_phase"] == "needs_user"
        assert row["runtime_source"] == "managed_local_transport"
        assert row["confidence"] == "live"
        assert row["runtime_display"]["truth_tier"] == "managed-local"
        assert row["runtime_display"]["signal_tier"] == "phase_signal"
        assert row["runtime_display"]["headline"] == "Idle"
        assert row["runtime_display"]["detail"] == "Waiting for next prompt"
        assert row["runtime_display"]["tone"] == "idle"
        assert row["runtime_display"]["needs_attention"] is False


def test_sessions_list_marks_recent_managed_idle_with_missing_assistant_as_syncing(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_syncing.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="runtime-syncing",
            user_messages=2,
            assistant_messages=1,
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="claude_channel_bridge",
            source_runner_id=1,
            managed_session_name="claude",
        )
        session.last_activity_at = now - timedelta(milliseconds=500)
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{session.id}",
                session_id=session.id,
                provider="claude",
                device_id="cinder",
                phase="idle",
                phase_source="managed_local_transport",
                active_tool=None,
                phase_started_at=now,
                last_runtime_signal_at=now,
                last_progress_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now + timedelta(minutes=10),
                terminal_state=None,
                terminal_at=None,
                runtime_version=4,
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=1", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["id"] == str(session.id)
        assert row["presence_state"] == "idle"
        assert row["runtime_display"]["state"] == "syncing_transcript"
        assert row["runtime_display"]["headline"] == "Syncing"
        assert row["runtime_display"]["phase_label"] == "Syncing transcript"
        assert row["runtime_display"]["tone"] == "active"
        assert row["runtime_display"]["is_idle"] is False
        assert row["timeline_card"]["status"]["label"] == "Syncing"
        assert row["timeline_card"]["status"]["tone"] == "active"


def test_sessions_list_marks_managed_idle_after_unanswered_latest_user_as_syncing(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_syncing_unanswered_user.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="runtime-syncing-unanswered",
            user_messages=45,
            assistant_messages=113,
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="claude_channel_bridge",
            source_runner_id=1,
            managed_session_name="claude",
        )
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session.id,
                provider="claude",
                environment="production",
                project="runtime-syncing-unanswered",
                started_at=session.started_at,
                execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="previous response",
                        timestamp=now - timedelta(seconds=30),
                        source_path="/tmp/claude-syncing.jsonl",
                        source_offset=1,
                        raw_json='{"type":"assistant"}',
                    ),
                    EventIngest(
                        role="user",
                        content_text="latest prompt with no response yet",
                        timestamp=now - timedelta(seconds=20),
                        source_path="/tmp/claude-syncing.jsonl",
                        source_offset=2,
                        raw_json='{"type":"user"}',
                    ),
                    EventIngest(
                        role="system",
                        content_text="File history snapshot",
                        timestamp=now - timedelta(seconds=19),
                        source_path="/tmp/claude-syncing.jsonl",
                        source_offset=3,
                        raw_json='{"type":"system"}',
                    ),
                ],
            )
        )
        session.user_messages = 45
        session.assistant_messages = 113
        session.last_activity_at = now - timedelta(seconds=19)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"claude:{session.id}",
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(seconds=18),
                    freshness_ms=90_000,
                    dedupe_key=f"{session.id}:thinking",
                ),
                RuntimeEventIngest(
                    runtime_key=f"claude:{session.id}",
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now,
                    freshness_ms=600_000,
                    dedupe_key=f"{session.id}:idle",
                ),
            ],
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get(
            "/agents/sessions?project=runtime-syncing-unanswered&provider=claude&limit=5",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["assistant_messages"] == 113
        assert row["user_messages"] == 45
        assert row["presence_state"] == "idle"
        assert row["runtime_display"]["state"] == "syncing_transcript"
        assert row["runtime_display"]["phase_label"] == "Syncing transcript"
        assert row["runtime_display"]["is_idle"] is False


def test_sessions_list_does_not_infer_syncing_without_post_prompt_active_phase(tmp_path):
    factory = _make_db(tmp_path, "materialized_runtime_no_syncing_without_active.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="runtime-not-syncing-unanswered",
            user_messages=45,
            assistant_messages=113,
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            managed_transport="claude_channel_bridge",
            source_runner_id=1,
            managed_session_name="claude",
        )
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session.id,
                provider="claude",
                environment="production",
                project="runtime-not-syncing-unanswered",
                started_at=session.started_at,
                execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="previous response",
                        timestamp=now - timedelta(seconds=30),
                        source_path="/tmp/claude-no-syncing.jsonl",
                        source_offset=1,
                        raw_json='{"type":"assistant"}',
                    ),
                    EventIngest(
                        role="user",
                        content_text="latest prompt with no active phase",
                        timestamp=now - timedelta(seconds=20),
                        source_path="/tmp/claude-no-syncing.jsonl",
                        source_offset=2,
                        raw_json='{"type":"user"}',
                    ),
                ],
            )
        )
        session.user_messages = 45
        session.assistant_messages = 113
        session.last_activity_at = now - timedelta(seconds=20)
        _upsert_runtime_state(
            db,
            session_id=str(session.id),
            phase="idle",
            updated_at=now,
            freshness_window=timedelta(minutes=10),
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get(
            "/agents/sessions?project=runtime-not-syncing-unanswered&provider=claude&limit=5",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text
        row = resp.json()["sessions"][0]
        assert row["presence_state"] == "idle"
        assert row["runtime_display"]["state"] == "idle"
        assert row["runtime_display"]["phase_label"] == "Idle"
        assert row["runtime_display"]["is_idle"] is True


@pytest.mark.skip(reason="UnmanagedSessionBinding table was removed; replacement uses kernel SessionConnection rows")
def test_active_sessions_online_process_binding_keeps_needs_user_idle(tmp_path):
    factory = _make_db(tmp_path, "active_process_binding_attention.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=None,
            project="online-unmanaged",
        )
        _upsert_runtime_state(
            db,
            session_id=str(session.id),
            phase="needs_user",
            updated_at=now - timedelta(seconds=20),
        )
        db.add(
            AgentHeartbeat(
                device_id="timeline-runtime",
                received_at=now,
            )
        )
        db.add(
            UnmanagedSessionBinding(
                machine_id="dev-machine",
                device_id="timeline-runtime",
                provider="claude",
                provider_session_id=str(session.id),
                session_id=session.id,
                source_path="/tmp/session.jsonl",
                pid=1234,
                process_start_time=now - timedelta(hours=1),
                observed_at=now,
                last_seen_at=now,
                source_mtime=now,
                binding_state="observed",
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = next(item for item in resp.json()["sessions"] if item["id"] == str(session.id))
        assert row["runtime_display"]["control_path"] == "unmanaged"
        assert row["runtime_display"]["signal_tier"] == "process_binding"
        assert row["runtime_display"]["host_state"] == "online"
        assert row["runtime_display"]["state"] == "needs_user"
        assert row["runtime_display"]["phase_label"] == "Idle"
        assert row["runtime_display"]["needs_attention"] is False

        list_resp = client.get("/agents/sessions?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert list_resp.status_code == 200, list_resp.text
        list_row = next(item for item in list_resp.json()["sessions"] if item["id"] == str(session.id))
        assert list_row["timeline_card"]["status"]["label"] == "Idle"


@pytest.mark.skip(reason="UnmanagedSessionBinding table was removed; replacement uses kernel SessionConnection rows")
def test_sessions_list_process_observed_without_phase_renders_running_process(tmp_path):
    factory = _make_db(tmp_path, "process_observed_without_phase.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=None,
            project="process-only",
        )
        db.add(
            AgentHeartbeat(
                device_id="timeline-runtime",
                received_at=now,
            )
        )
        db.add(
            UnmanagedSessionBinding(
                machine_id="dev-machine",
                device_id="timeline-runtime",
                provider="claude",
                provider_session_id=str(session.id),
                session_id=session.id,
                source_path="/tmp/session.jsonl",
                pid=1234,
                process_start_time=now - timedelta(hours=1),
                observed_at=now,
                last_seen_at=now,
                source_mtime=now,
                binding_state="observed",
            )
        )
        db.commit()
    finally:
        db.close()

    for client in _client(factory):
        resp = client.get("/agents/sessions?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200, resp.text
        row = next(item for item in resp.json()["sessions"] if item["id"] == str(session.id))
        assert row["timeline_card"]["status"]["label"] == "Running"
        assert row["timeline_card"]["status"]["seen_at_prefix"] == "Verified"


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
        assert row["status"] == "idle"
        assert row["presence_state"] is None
        assert row["display_phase"] == "Inactive"
        assert row["runtime_phase"] is None
        assert row["confidence"] is None
        assert row["runtime_display"]["truth_tier"] == "stale"
        assert row["runtime_display"]["headline"] == "Inactive"
        assert row["runtime_display"]["phase_label"] == "Inactive"


def test_sessions_surfaces_ignore_stale_presence_payload_after_newer_blocked_signal(tmp_path):
    factory = _make_db(tmp_path, "stale_presence_surface.db")
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        session = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=None,
            project="stale-presence-surface",
        )
    finally:
        db.close()

    for client in _client(factory):
        blocked = client.post(
            "/agents/presence",
            json={
                "session_id": str(session.id),
                "state": "blocked",
                "tool_name": "Bash",
                "provider": "claude",
                "occurred_at": now.isoformat(),
                "dedupe_key": "blocked-new",
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert blocked.status_code == 204, blocked.text

        stale_idle = client.post(
            "/agents/presence",
            json={
                "session_id": str(session.id),
                "state": "idle",
                "provider": "claude",
                "occurred_at": (now - timedelta(seconds=30)).isoformat(),
                "dedupe_key": "idle-old",
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert stale_idle.status_code == 204, stale_idle.text

        active_resp = client.get("/agents/sessions/active?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert active_resp.status_code == 200, active_resp.text
        active_row = next(item for item in active_resp.json()["sessions"] if item["id"] == str(session.id))
        assert active_row["status"] == "active"
        assert active_row["presence_state"] == "blocked"
        assert active_row["display_phase"] == "Blocked on Bash"
        assert active_row["runtime_phase"] == "blocked"
        assert active_row["confidence"] == "live"

        list_resp = client.get("/agents/sessions?days_back=14&limit=5", headers={"X-Agents-Token": "dev"})
        assert list_resp.status_code == 200, list_resp.text
        list_row = next(item for item in list_resp.json()["sessions"] if item["id"] == str(session.id))
        assert list_row["presence_state"] == "blocked"
        assert list_row["display_phase"] == "Blocked on Bash"
