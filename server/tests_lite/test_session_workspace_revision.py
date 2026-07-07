"""Tests for durable session viewport revision fingerprints."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionLivePreview
from zerg.models.agents import SessionRuntimeState
from zerg.services.session_pause_requests import resolve_pause_request
from zerg.services.session_pause_requests import upsert_pause_request
from zerg.services.session_workspace import build_session_mobile_tail
from zerg.services.session_workspace import build_session_workspace
from zerg.services.session_workspace_revision import load_session_workspace_revision


def _make_db(tmp_path, name="session_workspace_revision.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, provider: str = "claude") -> AgentSession:
    session = AgentSession(
        provider=provider,
        environment="production",
        project="test",
        started_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=0,
    )
    db.add(session)
    db.flush()
    return session


def test_workspace_revision_tracks_pause_request_create_and_resolve(tmp_path):
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = _seed_session(db)
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None
        assert initial.pause_request_count == 0

        pause, _changed = upsert_pause_request(
            db,
            session_id=session_id,
            runtime_key="claude:session-1",
            provider="claude",
            request_key="claude:session-1:question-1",
            provider_request_id="question-1",
            title="Approval",
            request_payload={"questions": [{"id": "approval", "question": "Proceed?"}]},
            can_respond=True,
            occurred_at=now,
        )
        db.commit()

        pending = load_session_workspace_revision(db, session_id)
        assert pending is not None
        assert pending.pause_request_count == 1
        assert pending.pause_request_fingerprint is not None
        assert pending.fingerprint != initial.fingerprint

        _same_pause, changed = upsert_pause_request(
            db,
            session_id=session_id,
            runtime_key="claude:session-1",
            provider="claude",
            request_key="claude:session-1:question-1",
            provider_request_id="question-1",
            title="Approval",
            request_payload={"questions": [{"id": "approval", "question": "Proceed?"}]},
            can_respond=True,
            occurred_at=now + timedelta(seconds=10),
        )
        assert changed is True
        db.commit()

        reobserved = load_session_workspace_revision(db, session_id)
        assert reobserved is not None
        assert reobserved.pause_request_fingerprint == pending.pause_request_fingerprint
        assert reobserved.fingerprint == pending.fingerprint

        resolve_pause_request(db, pause_request_id=pause.id, status="resolved", occurred_at=now + timedelta(seconds=1))
        db.commit()

        resolved = load_session_workspace_revision(db, session_id)
        assert resolved is not None
        assert resolved.pause_request_count == 0
        assert resolved.pause_request_fingerprint is None
        assert resolved.fingerprint != pending.fingerprint


def test_workspace_revision_ignores_legacy_hook_placeholders(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_hook_placeholder.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = _seed_session(db)
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None

        upsert_pause_request(
            db,
            session_id=session_id,
            runtime_key=f"claude:{session_id}",
            provider="claude",
            request_key=f"claude-hook:claude:{session_id}:AskUserQuestion",
            provider_request_id="claude-hook-ask-user-question",
            provider_ref={"source": "claude_hook"},
            title="Claude needs an answer",
            request_payload={"questions": [{"id": "terminal_answer", "question": "Claude is waiting."}]},
            can_respond=False,
            occurred_at=now,
        )
        db.commit()

        with_placeholder = load_session_workspace_revision(db, session_id)
        assert with_placeholder is not None
        assert with_placeholder.pause_request_count == 0
        assert with_placeholder.pause_request_fingerprint is None
        assert with_placeholder.fingerprint == initial.fingerprint


def test_workspace_revision_tracks_managed_control_changes(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_control.db")

    with sf() as db:
        session = _seed_session(db)
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None
        assert initial.managed_control_count == 0

        _thread, _run, conn = seed_managed_kernel_rows(db, session, state="attached")
        db.commit()

        attached = load_session_workspace_revision(db, session_id)
        assert attached is not None
        assert attached.managed_control_count == 1
        assert attached.managed_control_fingerprint is not None
        assert attached.fingerprint != initial.fingerprint

        _losing_thread, _losing_run, losing_conn = seed_managed_kernel_rows(db, session, state="detached")
        db.commit()

        with_losing_connection = load_session_workspace_revision(db, session_id)
        assert with_losing_connection is not None
        assert with_losing_connection.managed_control_count == 1
        assert with_losing_connection.managed_control_fingerprint == attached.managed_control_fingerprint
        assert with_losing_connection.fingerprint == attached.fingerprint

        losing_conn.last_health_at = datetime.now(timezone.utc) + timedelta(seconds=3)
        db.add(losing_conn)
        db.commit()

        losing_connection_ticked = load_session_workspace_revision(db, session_id)
        assert losing_connection_ticked is not None
        assert losing_connection_ticked.managed_control_fingerprint == attached.managed_control_fingerprint
        assert losing_connection_ticked.fingerprint == attached.fingerprint

        conn.state = "degraded"
        conn.last_health_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        db.add(conn)
        db.commit()

        degraded = load_session_workspace_revision(db, session_id)
        assert degraded is not None
        assert degraded.managed_control_count == 1
        assert degraded.managed_control_fingerprint != attached.managed_control_fingerprint
        assert degraded.fingerprint != attached.fingerprint


def test_workspace_revision_tracks_runtime_state_updates(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_runtime.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = _seed_session(db)
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None
        assert initial.latest_runtime_signal_at is None
        assert initial.runtime_version_sum == 0

        runtime = SessionRuntimeState(
            runtime_key=f"claude:{session_id}",
            session_id=session_id,
            provider="claude",
            phase="running",
            phase_source="runtime_event",
            timeline_anchor_at=now,
            updated_at=now,
            runtime_version=1,
        )
        db.add(runtime)
        db.commit()

        running = load_session_workspace_revision(db, session_id)
        assert running is not None
        assert running.latest_runtime_signal_at == now
        assert running.runtime_version_sum == 1
        assert running.fingerprint != initial.fingerprint

        runtime.runtime_version = 2
        runtime.updated_at = now + timedelta(seconds=5)
        db.add(runtime)
        db.commit()

        updated = load_session_workspace_revision(db, session_id)
        assert updated is not None
        assert updated.runtime_version_sum == 2
        assert updated.fingerprint != running.fingerprint


def test_workspace_revision_tracks_live_preview_updates(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_preview.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = _seed_session(db, provider="codex")
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None
        assert initial.live_preview_updated_at is None

        db.add(
            SessionLivePreview(
                session_id=session_id,
                thread_id="thread-1",
                turn_key=f"codex_bridge_live:{session_id}:thread-1:turn-1",
                seq=1,
                preview_text="hello live",
                provisional_cursor=f"codex_bridge_live:{session_id}:thread-1:turn-1:1",
                provisional_complete=0,
                event_origin="live_provisional",
                preview_observed_at=now,
                preview_updated_at=now,
                source="codex_bridge_live",
                last_observation_id=f"runtime:codex_bridge_live:preview:{session_id}:1",
            )
        )
        db.commit()

        preview = load_session_workspace_revision(db, session_id)
        assert preview is not None
        assert preview.live_preview_updated_at == now
        assert preview.fingerprint != initial.fingerprint


def test_workspace_revision_is_stable_for_identical_reads(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_stable.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = _seed_session(db)
        db.add(AgentEvent(session_id=str(session.id), role="assistant", content_text="hello", timestamp=now))
        db.commit()
        session_id = session.id

        first = load_session_workspace_revision(db, session_id)
        second = load_session_workspace_revision(db, session_id)

        assert first is not None
        assert second is not None
        assert second.signature == first.signature
        assert second.fingerprint == first.fingerprint


def test_workspace_revision_tracks_anchor_title_changes(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_title.db")

    with sf() as db:
        session = _seed_session(db)
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None

        session.anchor_title = "Realtime Session Titles"
        session.updated_at = initial.latest_session_updated_at
        db.add(session)
        db.commit()

        titled = load_session_workspace_revision(db, session_id)
        assert titled is not None
        assert titled.fingerprint != initial.fingerprint


def test_workspace_revision_does_not_track_non_anchor_title_inputs(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_non_anchor_title.db")

    with sf() as db:
        session = _seed_session(db)
        db.commit()
        session_id = session.id

        initial = load_session_workspace_revision(db, session_id)
        assert initial is not None

        session.summary_title = "Drifting Summary Title"
        session.first_user_message_preview = "Please add realtime titles."
        session.summary_revision = 99
        session.updated_at = initial.latest_session_updated_at
        db.add(session)
        db.commit()

        changed = load_session_workspace_revision(db, session_id)
        assert changed is not None
        assert changed.fingerprint == initial.fingerprint


def test_workspace_responses_include_matching_revision(tmp_path):
    sf = _make_db(tmp_path, name="session_workspace_revision_response.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = _seed_session(db)
        db.add(AgentEvent(session_id=str(session.id), role="assistant", content_text="hello", timestamp=now))
        db.commit()
        session_id = session.id

        workspace = build_session_workspace(db=db, session_id=session_id, limit=10)
        mobile_tail = build_session_mobile_tail(db=db, session_id=session_id, limit=10)

        assert workspace.workspace_revision.fingerprint
        assert mobile_tail.workspace_revision.fingerprint == workspace.workspace_revision.fingerprint
        assert mobile_tail.workspace_revision.latest_event_id == workspace.workspace_revision.latest_event_id
        assert mobile_tail.workspace_revision.thread_session_count == 1
