"""Adapter from the kernel projection to the legacy capability dataclass.

The adapter is the only translation layer between
``project_session_capabilities`` and the legacy ``SessionCapabilityFlags``
that the existing call sites (session views, chat, current control, APNS)
still consume. These tests pin the mapping so callers see the same shape
they got from ``build_session_capabilities`` once the swap lands.

See docs/specs/session-identity-kernel.md (Phase 4 sub-commit B, step 2).
"""

from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.services.agents.kernel_capability_adapter import build_session_capabilities_from_kernel
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome


def _engine(tmp_path):
    db_path = tmp_path / "kernel_adapter.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def db(tmp_path):
    engine = _engine(tmp_path)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        yield db


def _make_session(db, *, provider="codex"):
    s = AgentSession(
        provider=provider,
        environment="test",
        project="zerg",
        device_id="dev",
        started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


def _make_thread(db, session, *, primary=1):
    t = SessionThread(
        session_id=session.id,
        provider=session.provider,
        branch_kind="root",
        is_primary=primary,
    )
    db.add(t)
    db.flush()
    return t


def _make_run(db, thread, **overrides):
    r = SessionRun(
        thread_id=thread.id,
        provider=thread.provider,
        host_id=overrides.pop("host_id", "laptop-1"),
        launch_origin=overrides.pop("launch_origin", "longhouse_spawned"),
        started_at=overrides.pop("started_at", datetime.now(timezone.utc)),
        ended_at=overrides.pop("ended_at", None),
    )
    db.add(r)
    db.flush()
    return r


def _make_conn(db, run, *, control_plane="codex_bridge", state="attached", caps=None, **overrides):
    caps = caps or {}
    c = SessionConnection(
        run_id=run.id,
        control_plane=control_plane,
        acquisition_kind=overrides.pop("acquisition_kind", "spawned_control"),
        state=state,
        can_send_input=int(caps.get("send", 0)),
        can_interrupt=int(caps.get("interrupt", 0)),
        can_terminate=int(caps.get("terminate", 0)),
        can_tail_output=int(caps.get("tail", 0)),
        can_resume=int(caps.get("resume", 0)),
        last_health_at=overrides.pop("last_health_at", datetime.now(timezone.utc)),
    )
    db.add(c)
    db.flush()
    return c


def test_imported_session_returns_unmanaged_flags(db):
    """No thread, no kernel rows — adapter returns a fully unmanaged shape."""
    s = _make_session(db)
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is False
    assert flags.host_reattach_available is False
    assert flags.reply_to_live_session_available is False
    assert flags.can_queue_next_input is False
    assert flags.can_steer_active_turn is False
    assert flags.execution_home == SessionExecutionHome.UNMANAGED_LOCAL
    assert flags.managed_transport is None
    assert flags.home_label is None


def test_none_session_returns_unmanaged_flags(db):
    flags = build_session_capabilities_from_kernel(db, None)
    assert flags.live_control_available is False
    assert flags.host_reattach_available is False
    assert flags.execution_home == SessionExecutionHome.UNMANAGED_LOCAL
    assert flags.managed_transport is None


def test_managed_codex_attached_grants_steer_and_send(db):
    """Codex bridge attached with send capability — full live control."""
    s = _make_session(db, provider="codex")
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, control_plane="codex_bridge", state="attached", caps={"send": 1, "interrupt": 1, "tail": 1})
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is True
    # A live-attached session is also reattachable — both buckets imply a
    # steerable control plane that can be reattached if the live session
    # later goes stale.
    assert flags.host_reattach_available is True
    assert flags.reply_to_live_session_available is True
    assert flags.can_queue_next_input is True
    assert flags.can_steer_active_turn is True
    assert flags.execution_home == SessionExecutionHome.MANAGED_LOCAL
    assert flags.managed_transport == ManagedSessionTransport.CODEX_APP_SERVER
    assert flags.home_label == "On this Mac"


def test_managed_claude_attached_does_not_grant_steer(db):
    """Steer is Codex-bridge-only today; Claude bridge gets reply but no steer."""
    s = _make_session(db, provider="claude")
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, control_plane="claude_channel_bridge", state="attached", caps={"send": 1, "tail": 1})
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is True
    assert flags.reply_to_live_session_available is True
    assert flags.can_queue_next_input is True
    assert flags.can_steer_active_turn is False
    assert flags.managed_transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE


def test_live_without_send_capability_drops_reply(db):
    """An attached connection without can_send_input must not advertise reply/queue."""
    s = _make_session(db, provider="codex")
    t = _make_thread(db, s)
    r = _make_run(db, t)
    # Steerable bits granted via can_interrupt only — kernel will still call
    # this 'live' (steerable kind), but the adapter must hide the reply
    # affordance because send is off.
    _make_conn(db, r, control_plane="codex_bridge", state="attached", caps={"interrupt": 1, "tail": 1})
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is True
    assert flags.reply_to_live_session_available is False
    assert flags.can_queue_next_input is False
    # Steer is gated on liveness only, not on send — keep parity with prior behavior.
    assert flags.can_steer_active_turn is True


def test_managed_detached_grants_reattach_only(db):
    s = _make_session(db, provider="codex")
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, control_plane="codex_bridge", state="detached", caps={"send": 1, "tail": 1})
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is False
    assert flags.host_reattach_available is True
    assert flags.reply_to_live_session_available is False
    assert flags.can_queue_next_input is False
    assert flags.can_steer_active_turn is False
    assert flags.execution_home == SessionExecutionHome.MANAGED_LOCAL
    assert flags.managed_transport == ManagedSessionTransport.CODEX_APP_SERVER


def test_observe_only_log_tail_returns_unmanaged(db):
    """log_tail observe_only is search-only; never managed in the legacy enum."""
    s = _make_session(db, provider="codex")
    t = _make_thread(db, s)
    r = _make_run(db, t, launch_origin="external_adopted")
    _make_conn(
        db, r,
        control_plane="log_tail",
        acquisition_kind="observe_only",
        state="attached",
        caps={"tail": 1},
    )
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is False
    assert flags.host_reattach_available is False
    assert flags.reply_to_live_session_available is False
    assert flags.can_queue_next_input is False
    assert flags.can_steer_active_turn is False
    assert flags.execution_home == SessionExecutionHome.UNMANAGED_LOCAL
    assert flags.managed_transport is None
    assert flags.home_label is None


def test_run_ended_dropps_back_to_unmanaged(db):
    """A closed run, even with stale attached connection, projects unmanaged."""
    s = _make_session(db, provider="codex")
    t = _make_thread(db, s)
    r = _make_run(db, t, ended_at=datetime.now(timezone.utc))
    _make_conn(db, r, control_plane="codex_bridge", state="attached", caps={"send": 1, "tail": 1})
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is False
    assert flags.host_reattach_available is False
    assert flags.reply_to_live_session_available is False
    assert flags.can_queue_next_input is False
    assert flags.can_steer_active_turn is False
    assert flags.managed_transport is None


def test_unknown_control_plane_keeps_live_but_drops_transport(db):
    """A future control plane string the adapter doesn't recognize: live is honored,
    but the legacy transport enum stays None — callers that need the enum
    must not assume coverage of unknown planes."""
    s = _make_session(db, provider="codex")
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, control_plane="future_plane", state="attached", caps={"send": 1, "tail": 1})
    db.commit()

    flags = build_session_capabilities_from_kernel(db, s)

    assert flags.live_control_available is True
    assert flags.reply_to_live_session_available is True
    assert flags.managed_transport is None
    # Steer requires the codex_bridge plane, not just provider == codex.
    assert flags.can_steer_active_turn is False


def test_adapter_does_not_read_legacy_columns(db):
    """Garbage in legacy fields must not leak into the adapter output.

    If the adapter ever consults ``session.execution_home`` or
    ``session.managed_transport``, this test would surface either by
    flipping a flag or raising a coercion error.
    """
    s = _make_session(db, provider="codex")
    s.execution_home = "managed_local"
    s.managed_transport = "codex_app_server"
    s.source_runner_id = 123
    s.source_runner_name = "fake-runner"
    db.commit()

    # No kernel rows at all — adapter must report unmanaged regardless of
    # what the legacy columns claim.
    flags = build_session_capabilities_from_kernel(db, s)
    assert flags.live_control_available is False
    assert flags.host_reattach_available is False
    assert flags.execution_home == SessionExecutionHome.UNMANAGED_LOCAL
    assert flags.managed_transport is None
