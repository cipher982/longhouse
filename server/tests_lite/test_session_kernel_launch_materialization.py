"""Phase 2 dual-write: launch paths materialize kernel rows.

These tests assert that the managed-local launcher and the remote-session
launcher each create the four-noun kernel rows (thread, alias, launch
attempt, run, connection) alongside the legacy ``AgentSession`` row.

See docs/specs/session-identity-kernel.md.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
from unittest.mock import MagicMock

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_connection
from zerg.services.agents.kernel_writes import record_launch_attempt
from zerg.services.agents.kernel_writes import record_run
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.agents.kernel_writes import update_launch_attempt
from sqlalchemy.orm import sessionmaker


def _session(tmp_path):
    db_path = tmp_path / "kernel.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def _make_session_row(db, *, provider="codex"):
    from uuid import uuid4

    sid = uuid4()
    row = AgentSession(
        id=sid,
        provider=provider,
        environment="development",
        project="proj",
        device_id="laptop-1",
        cwd="/tmp/proj",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(sid),
        thread_root_session_id=sid,
        continuation_kind="local",
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
    )
    db.add(row)
    db.flush()
    return row


def test_launch_attempt_idempotent_on_client_request_id(tmp_path):
    db = _session(tmp_path)
    session = _make_session_row(db)
    thread = ensure_primary_thread(db, session)
    a1 = record_launch_attempt(
        db,
        session=session,
        thread=thread,
        provider="codex",
        host_id="laptop-1",
        client_request_id="req-1",
        command_id="cmd-1",
    )
    a2 = record_launch_attempt(
        db,
        session=session,
        thread=thread,
        provider="codex",
        host_id="laptop-1",
        client_request_id="req-1",
        command_id="cmd-2",  # different — must be ignored, idempotent on req-1
    )
    assert a1.id == a2.id

    rows = db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.session_id == session.id).all()
    assert len(rows) == 1


def test_run_and_connection_materialize_under_thread(tmp_path):
    db = _session(tmp_path)
    session = _make_session_row(db, provider="claude")
    thread = ensure_primary_thread(db, session)
    record_thread_alias(
        db,
        thread=thread,
        provider="claude",
        alias_kind="provider_session_id",
        alias_value=str(session.id),
    )
    run = record_run(db, thread=thread, provider="claude", host_id="laptop-1", cwd="/tmp/proj")
    record_connection(
        db,
        run=run,
        control_plane="pty",
        acquisition_kind="spawned_control",
        state="attached",
        can_send_input=1,
    )
    db.commit()

    aliases = db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id == thread.id).all()
    assert len(aliases) == 1
    assert aliases[0].alias_value == str(session.id)

    runs = db.query(SessionRun).filter(SessionRun.thread_id == thread.id).all()
    assert len(runs) == 1

    conns = db.query(SessionConnection).filter(SessionConnection.run_id == runs[0].id).all()
    assert len(conns) == 1
    assert conns[0].state == "attached"
    assert conns[0].can_send_input == 1


def test_record_thread_alias_is_idempotent(tmp_path):
    db = _session(tmp_path)
    session = _make_session_row(db)
    thread = ensure_primary_thread(db, session)
    record_thread_alias(
        db,
        thread=thread,
        provider="codex",
        alias_kind="provider_session_id",
        alias_value="abc",
    )
    record_thread_alias(
        db,
        thread=thread,
        provider="codex",
        alias_kind="provider_session_id",
        alias_value="abc",
    )
    db.commit()
    rows = db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id == thread.id).all()
    assert len(rows) == 1


def test_upsert_connection_idempotent_per_run_and_plane(tmp_path):
    from zerg.services.agents.kernel_writes import ensure_open_run_for_session
    from zerg.services.agents.kernel_writes import upsert_connection_for_run

    db = _session(tmp_path)
    session = _make_session_row(db, provider="codex")
    run = ensure_open_run_for_session(db, session)

    c1 = upsert_connection_for_run(
        db,
        run=run,
        control_plane="codex_bridge",
        acquisition_kind="adopted_control",
        state="attached",
        can_send_input=1,
    )
    c2 = upsert_connection_for_run(
        db,
        run=run,
        control_plane="codex_bridge",
        acquisition_kind="adopted_control",
        state="degraded",
    )
    db.commit()

    assert c1.id == c2.id
    rows = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).all()
    assert len(rows) == 1
    assert rows[0].state == "degraded"
    # capability bits left alone when caller passes None
    assert rows[0].can_send_input == 1


def test_open_run_reused_by_external_adoption(tmp_path):
    from zerg.services.agents.kernel_writes import ensure_open_run_for_session

    db = _session(tmp_path)
    session = _make_session_row(db)
    r1 = ensure_open_run_for_session(db, session, launch_origin="external_adopted")
    r2 = ensure_open_run_for_session(db, session, launch_origin="external_adopted")
    db.commit()

    assert r1.id == r2.id


def test_bridge_offline_does_not_create_phantom_run(tmp_path):
    """Negative bridge evidence must not fabricate a run for unknown sessions."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.services.managed_control_state import _mirror_connection_state

    db = _session(tmp_path)
    session = _make_session_row(db, provider="codex")
    db.commit()

    # No prior run/connection — offline mirror must skip rather than adopt.
    _mirror_connection_state(
        db,
        session_id=session.id,
        provider="codex",
        control_state="offline",
        external_name=None,
        device_id="laptop-1",
    )
    db.commit()

    assert db.query(SessionRun).count() == 0
    assert db.query(SessionConnection).count() == 0


def test_bridge_online_then_offline_flips_existing_connection(tmp_path):
    from zerg.models.agents import SessionConnection
    from zerg.services.managed_control_state import _mirror_connection_state

    db = _session(tmp_path)
    session = _make_session_row(db, provider="codex")
    db.commit()

    _mirror_connection_state(
        db,
        session_id=session.id,
        provider="codex",
        control_state="online",
        external_name="laptop-1",
        device_id="laptop-1",
    )
    db.commit()
    online = db.query(SessionConnection).one()
    assert online.state == "attached"

    _mirror_connection_state(
        db,
        session_id=session.id,
        provider="codex",
        control_state="offline",
        external_name=None,
        device_id="laptop-1",
    )
    db.commit()
    rows = db.query(SessionConnection).all()
    assert len(rows) == 1, "should reuse existing connection, not create a new one"
    assert rows[0].id == online.id
    assert rows[0].state == "detached"
    assert rows[0].released_at is not None


def test_provider_thread_switched_lease_detaches_existing_connection(tmp_path):
    from types import SimpleNamespace

    from zerg.models.agents import SessionConnection
    from zerg.services.managed_control_state import _mirror_connection_state
    from zerg.services.managed_control_state import upsert_managed_control_leases

    db = _session(tmp_path)
    session = _make_session_row(db, provider="codex")
    db.commit()

    _mirror_connection_state(
        db,
        session_id=session.id,
        provider="codex",
        control_state="online",
        external_name="laptop-1",
        device_id="laptop-1",
    )
    db.commit()
    online = db.query(SessionConnection).one()
    assert online.state == "attached"

    upsert_managed_control_leases(
        db,
        [
            SimpleNamespace(
                session_id=session.id,
                provider="codex",
                state="degraded",
                bridge_status="degraded",
                thread_subscription_status="provider_thread_switched",
                machine_id="laptop-1",
            )
        ],
        device_id="laptop-1",
        received_at=datetime.now(timezone.utc),
    )
    db.commit()

    rows = db.query(SessionConnection).all()
    assert len(rows) == 1
    assert rows[0].id == online.id
    assert rows[0].state == "detached"


def test_update_launch_attempt_state_run_link(tmp_path):
    db = _session(tmp_path)
    session = _make_session_row(db)
    thread = ensure_primary_thread(db, session)
    attempt = record_launch_attempt(
        db,
        session=session,
        thread=thread,
        provider="codex",
        host_id="laptop-1",
        client_request_id="req-x",
        command_id="cmd-x",
    )
    run = record_run(db, thread=thread, provider="codex", host_id="laptop-1")
    update_launch_attempt(db, attempt, state="dispatched", run=run, clear_expires=True)
    db.commit()

    refreshed = db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.id == attempt.id).one()
    assert refreshed.state == "dispatched"
    assert refreshed.run_id == run.id
    assert refreshed.expires_at is None
